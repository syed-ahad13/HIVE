// ===========================================================================
// HIVE STUDIO — THE DEPLOY WIZARD
// ===========================================================================
// Three screens: Basics, Resources, Review. Every question comes from the
// template's `parameters`, and WHICH SCREEN a question appears on comes from
// that parameter's `group`. This file therefore knows how to render a kind of
// question, and nothing at all about which questions exist.
//
// ---------------------------------------------------------------------------
// WHAT CHANGED, AND WHY IT HAD TO
// ---------------------------------------------------------------------------
// This wizard used to be schema-driven in intent and hard-coded in fact: it
// named projectName, model, availability, tier and durationDays directly, which
// is every parameter `inference-endpoint` happens to declare. `rag-stack` asks
// nine questions — two of kinds that did not exist — so the choice was a second
// branch per template, or a renderer that reads the schema.
//
// Now: `paramsInGroup(params, "basics")` decides what Basics shows,
// `ParameterField` decides how each one looks, and validation is derived from
// the parameter's own type and bounds. Adding a question to a template adds an
// input here with no edit to this file.
//
// ---------------------------------------------------------------------------
// CLIENT-SIDE VALIDATION IS A COURTESY. THE SERVER IS THE CONTROL.
// ---------------------------------------------------------------------------
// Everything checked here is checked again by the BFF, and invariant (c) is
// explicit that the wizard's regex is a hint rather than a control. So this file
// aims at ONE thing: telling a PI about a problem the moment they can fix it,
// beside the field, rather than after they press Deploy.
//
// The two must agree, and where they cannot, the server wins visibly: a 400 from
// POST /api/deployments carries a list of {field, message}, and those are mapped
// straight back onto the same inputs the client would have flagged. A rejection
// never appears as a bare banner when it belongs to a field.
// ===========================================================================

import { useCallback, useEffect, useMemo, useState } from "react";
import { useNavigate, useParams, Link } from "react-router-dom";

import { ApiError, get, post } from "../api.js";
import { AmberBanner, Fetched, useExecutorMode, useFetch } from "../components.jsx";
import { ParameterField, humanBytes, paramsInGroup } from "../fields.jsx";
import { STEPS, stepLabel } from "../steps.js";
import { TIERS, TIER_ROWS } from "../tiers.js";

// Mirrors validation.RESERVED_NAMES / RESERVED_PREFIXES. Duplicated on purpose
// and only as a courtesy: the server unions these with whatever the template
// declares and is the thing that actually refuses.
const RESERVED_NAMES = ["inf-101", "inf-103", "default"];
const RESERVED_PREFIXES = ["kube-", "hive-"];

// ---------------------------------------------------------------------------
// VALIDATION — derived from the schema, mirroring app/validation.py
// ---------------------------------------------------------------------------

/**
 * Check one answer against its parameter. Returns a message, or null.
 *
 * Driven by `param.type` and the bounds the template declares, so a new question
 * is validated without anything being added here. The one name-specific rule is
 * the reserved-project-name check, and it is name-specific in the backend too —
 * `projectName` is the single parameter that becomes a namespace, a cluster name
 * and a hostname, which is why it carries rules no other parameter has.
 */
function checkParam(param, value) {
  const empty =
    value === undefined ||
    value === null ||
    (typeof value === "string" && value.trim() === "");

  if (empty) {
    if (param.type === "fixed" || param.type === "upload") return null;
    if (param.required && param.default == null) {
      return `${param.label} is needed.`;
    }
    return null;
  }

  if (param.type === "string") {
    const text = String(value).trim();
    if (param.pattern && !new RegExp(`^(?:${param.pattern})$`).test(text)) {
      // The rule in words, since a raw regex helps nobody. The template's `help`
      // says the same thing in prose and is rendered underneath.
      return `Use 3–20 characters: lowercase letters, numbers and hyphens only.`;
    }
    if (param.name === "projectName") {
      if (RESERVED_NAMES.includes(text)) {
        return `'${text}' is reserved by the platform. Choose another name.`;
      }
      const prefix = RESERVED_PREFIXES.find((p) => text.startsWith(p));
      if (prefix) {
        return `Names cannot start with '${prefix}' — that prefix is reserved.`;
      }
    }
  }

  if (param.type === "integer") {
    if (typeof value !== "number" || Number.isNaN(value)) {
      return `${param.label} must be a whole number.`;
    }
    if (param.minimum != null && value < param.minimum) {
      return `Between ${param.minimum} and ${param.maximum}.`;
    }
    if (param.maximum != null && value > param.maximum) {
      return `Between ${param.minimum} and ${param.maximum}.`;
    }
  }

  if (param.type === "enum") {
    const allowed = (param.values || []).map((o) => o.value);
    if (allowed.length && !allowed.includes(value)) {
      return `Choose one of the available options.`;
    }
  }

  // AN EMPTY LIST IS NOT AN ANSWER, and it does not look empty to the check at
  // the top of this function — `[]` is neither undefined, null, nor a blank
  // string. Without this a PI could pass the screen having chosen no columns
  // and be refused by the server at Deploy, two screens later, for a question
  // they had already walked past. The backend enforces the same rule; this is
  // the faster half of it, beside the field.
  if (param.type === "multiselect" && Array.isArray(value)) {
    if (param.required && value.length === 0) {
      return `${param.label} is needed — choose at least one.`;
    }
    const duplicated = value.filter((v, i) => value.indexOf(v) !== i);
    if (duplicated.length) {
      return `${duplicated[0]} is listed twice.`;
    }
  }

  return null;
}

function checkGroup(params, values, group) {
  const errors = {};
  for (const param of paramsInGroup(params, group)) {
    const message = checkParam(param, values[param.name]);
    if (message) errors[param.name] = message;
  }
  return errors;
}

// ---------------------------------------------------------------------------
// PIECES
// ---------------------------------------------------------------------------

function StepRail({ current }) {
  const labels = ["Basics", "Resources", "Review"];
  return (
    <ol className="rail">
      {labels.map((label, i) => (
        <li
          key={label}
          className={`rail__item${i === current ? " rail__item--now" : ""}${
            i < current ? " rail__item--done" : ""
          }`}
        >
          <span className="rail__n">{i < current ? "✓" : i + 1}</span>
          {label}
        </li>
      ))}
    </ol>
  );
}

/**
 * The two derived names a project name produces, shown while it can still change.
 *
 * It becomes the namespace and the endpoint hostname and cannot be changed
 * afterwards, so seeing both forms is worth more than any amount of explanation
 * about why.
 */
function DerivedNames({ name, mode }) {
  if (!name) return null;
  return (
    <div className="derived">
      <div>
        <span>Your space on the cluster</span>
        {/* DETERMINISTIC, so it can be shown as fact. `p-<project>` is what the
            namespace-pack fragment renders; there is no discovery involved. */}
        <code>p-{name}</code>
      </div>
      <div>
        <span>Your endpoint hostname</span>
        {/* NOT DETERMINISTIC, and this used to pretend otherwise — it read
            `<name>.mock.hive.local` in every mode, which is a made-up host on a
            made-up domain the moment the console is provisioning for real.
            Locally KServe publishes a `-predictor` segment that production will
            not have (DEVIATIONS.md), so the real hostname cannot be assembled
            here and must be read from `.status.url` once the service exists.
            Showing the DOMAIN is honest and still answers the question a PI is
            asking, which is "does my project name end up in the address?". */}
        {mode === "mock" ? (
          <code>{name}.mock.hive.local</code>
        ) : (
          <span className="small muted">
            built from <code>{name}</code> under <code>hive.local</code> — the exact
            address is read from your service once it is running
          </span>
        )}
      </div>
    </div>
  );
}

/** The ceiling a tier buys, updating live as the choice changes. */
function TierCeiling({ tier }) {
  const t = TIERS[tier] || TIERS.small;
  return (
    <>
      <div className="quota">
        <div className="quota__head">Your ceiling with {t.label}</div>
        <dl className="quota__grid">
          {TIER_ROWS.map(([label, key]) => (
            <div key={key}>
              <dt>{label}</dt>
              <dd>{t[key]}</dd>
            </div>
          ))}
        </dl>
        <p className="small muted">
          Your project cannot go above this. Requests beyond it are refused rather
          than queued.
        </p>
      </div>
      <AmberBanner>
        <strong>Both sizes are identical on this laptop.</strong> The GPUs are
        simulated. On the real cluster the sizes differ and the hardware is real.
      </AmberBanner>
    </>
  );
}

/**
 * What the server found inside the dataset, under the field that chose it.
 *
 * Shown for BOTH cases and says which: the PI's own upload, or the bundled
 * sample that stands in when they add nothing. The second is the one worth
 * being explicit about — the columns on the next screen are that file's
 * columns, and a PI who thought the question was about a file of theirs would
 * be choosing from a list that came from nowhere they can see.
 *
 * The row count is the SERVER'S ESTIMATE and is labelled as one: it is
 * extrapolated from a 64KB sample rather than counted, because counting means
 * reading the whole file to answer a question nobody is deciding anything on.
 */
function DatasetShape({ preview, sample }) {
  if (!preview) return null;
  const rows = preview.rowsExact
    ? `${preview.rowsEstimated.toLocaleString()} rows`
    : `about ${preview.rowsEstimated.toLocaleString()} rows`;
  return (
    <div className="quota">
      <div className="quota__head">
        {sample ? `The sample we will use — ${sample}` : "What we found in your file"}
      </div>
      <p className="small">
        {preview.format.toUpperCase()}, {rows},{" "}
        <b>{preview.columns.length}</b> column
        {preview.columns.length === 1 ? "" : "s"}: {preview.columns.join(", ")}
      </p>
      {sample && (
        <p className="small muted">
          You added no file, so your run uses this one. Add your own above to
          replace it — the next screen asks which of its columns the model reads.
        </p>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// THE TWO QUESTION SCREENS
// ---------------------------------------------------------------------------

/**
 * One screen's worth of parameters, rendered from the template.
 *
 * `after` is the ONE place presentation is specialised, and it is deliberately
 * limited to what follows a field rather than what replaces it: the derived
 * names under a project name, the ceiling under a tier. Both are read-only
 * consequences of an answer, and both are keyed by a parameter name that exists
 * in every template that has the concept. The INPUT is always the generic one.
 */
function QuestionScreen({ params, group, values, set, errors, touch, after, optionsFor }) {
  const mine = paramsInGroup(params, group);

  if (mine.length === 0) {
    return (
      <div className="card">
        <p className="small muted">
          This template asks nothing on this screen. Continue.
        </p>
      </div>
    );
  }

  return (
    <>
      {mine.map((param, i) => (
        <div className="card" key={param.name}>
          <ParameterField
            param={param}
            value={values[param.name]}
            onChange={(v) => set(param.name, v)}
            onBlur={() => touch(param.name)}
            error={errors[param.name]}
            autoFocus={i === 0 && group === "basics"}
            // Only a question whose choices come from outside the catalogue gets
            // these; everything else is undefined and renders from the
            // template's own `values`, exactly as before.
            options={optionsFor?.(param)}
          />
          {after?.(param, values, errors)}
        </div>
      ))}
    </>
  );
}

// ---------------------------------------------------------------------------
// REVIEW
// ---------------------------------------------------------------------------

/**
 * One rendered manifest, with the fragment's explanatory comments folded away.
 *
 * The fragments are heavily commented — deliberately, since every field in them
 * was established as load-bearing by an actual failure, and those notes are part
 * of what gets written to tenants/<project>/rendered/. But that means the first
 * thirty lines of every file are prose, and a PI who opens this panel to see
 * "what will be created" would find the manifest below the fold in a scroll box.
 *
 * So the DEFAULT VIEW hides comment lines and says exactly how many it hid. The
 * full text is one click away and is what actually lands on disk — the preview
 * is a view of the file, never a different file.
 */
function YamlFile({ file }) {
  const [full, setFull] = useState(false);
  const lines = file.content.split("\n");
  const stripped = lines.filter((l) => {
    const t = l.trim();
    return t !== "" && !t.startsWith("#");
  });
  const hidden = lines.length - stripped.length;
  const shown = full ? file.content : stripped.join("\n");

  return (
    <div>
      <div className="yaml__name">
        <span>{file.name}</span>
        {hidden > 0 && (
          <button type="button" className="yaml__toggle" onClick={() => setFull((v) => !v)}>
            {full ? "hide comments" : `show ${hidden} comment lines`}
          </button>
        )}
      </div>
      <pre className="yaml" tabIndex={0}>
        <code>{shown}</code>
      </pre>
    </div>
  );
}

/**
 * The documents this project will index — the corpus, or the honest fallback.
 *
 * Fetched from the SERVER's manifest rather than remembered from the upload
 * widget, for the same reason the widget shows the server's list: these are the
 * files that will actually be copied, and a review screen that listed something
 * else would be reviewing a different deployment.
 */
function CorpusSummary({ uploadId, collection }) {
  const [manifest, setManifest] = useState(null);
  const [error, setError] = useState(null);

  useEffect(() => {
    if (!uploadId) {
      setManifest(null);
      setError(null);
      return;
    }
    let cancelled = false;
    get(`/api/uploads/${uploadId}`)
      .then((body) => !cancelled && setManifest(body))
      .catch((err) => !cancelled && setError(err));
    return () => {
      cancelled = true;
    };
  }, [uploadId]);

  return (
    <div className="card">
      <h3>What will be indexed</h3>

      {!uploadId && (
        <>
          <p className="small">
            You did not add any documents, so your project will index the{" "}
            <b>bundled HIVE documents</b> — four short files about how this
            platform works. Enough to see it answering, and to check the passages
            it quotes.
          </p>
          <p className="small muted">
            To use your own instead, go back and add them.
          </p>
        </>
      )}

      {error && (
        <div className="state state--error">
          <div className="state__title">Could not list your documents</div>
          <p className="small">{error.message}</p>
        </div>
      )}

      {manifest && (
        <>
          <div className="filelist">
            <div className="filelist__head">
              <span>
                <b>{manifest.files.length}</b> document
                {manifest.files.length === 1 ? "" : "s"}
              </span>
              <span className="small muted">{humanBytes(manifest.totalBytes)}</span>
            </div>
            {manifest.files.map((f) => (
              <div className="filerow" key={f.name}>
                <span className="filerow__name">{f.name}</span>
                <span className="filerow__size small muted">{humanBytes(f.bytes)}</span>
              </div>
            ))}
          </div>
          <p className="small muted">
            Each answer will cite the file and passage it came from, using these
            names.
          </p>
        </>
      )}

      <div className="kv" style={{ marginTop: 10 }}>
        <span>Your search index</span>
        <b>
          <code>{collection}</code>
        </b>
      </div>
      <p className="small muted">
        Created inside your project. It goes when the project goes — there is no
        copy anywhere else.
      </p>
    </div>
  );
}

function Review({ template, values, models, files, filesError, filesLoading, onReloadFiles }) {
  const [open, setOpen] = useState(false);
  const params = template.spec.parameters || [];

  // THE PRE-DISCLOSED STEPS COME FROM THE TEMPLATE, not from a list in the
  // console. GET /api/templates/{id} returns the plan the backend will actually
  // run, computed from this template's own `resources` — so a RAG project shows
  // its eleven steps in its own order and nothing here had to learn about RAG.
  const steps = (template.steps || []).filter((id) => STEPS[id]);

  // Every answer, in the template's own order, rendered from the template's own
  // labels. The old version listed five hard-coded rows and would have shown a
  // RAG project as if it had no documents, no index and no top-k.
  const summary = params
    .filter((p) => p.type !== "upload")
    .map((p) => {
      const raw = values[p.name];
      let shown = raw;
      if (p.type === "enum") {
        const option = (p.values || []).find((o) => o.value === raw);
        shown = option ? option.label : raw;
        // A model id is not a name a PI recognises; the catalogue's displayName is.
        const model = models.find((m) => m.id === raw);
        if (model) shown = model.displayName;
      }
      if (p.name === "durationDays") shown = `${raw} days, then removed automatically`;
      return [p.label, shown ?? "—"];
    });

  const isRag = template.spec.interface === "rag";

  return (
    <>
      <div className="card">
        <h3>Your project</h3>
        <div className="tablewrap">
          <table>
            <tbody>
              {summary.map(([k, v]) => (
                <tr key={k}>
                  <td style={{ width: "38%" }}>
                    <b>{k}</b>
                  </td>
                  <td>{v}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>

      {isRag && (
        <CorpusSummary
          uploadId={(values.corpusUploadId || "").trim()}
          collection={`${values.projectName}-corpus`}
        />
      )}

      <div className="card">
        <h3>What will happen</h3>
        <div className="steps">
          {steps.map((id, i) => (
            <div className="steps__row" key={id}>
              <div className="steps__n">{i + 1}</div>
              <div className="steps__body">
                <b>{stepLabel(id)}</b>
                <span>{STEPS[id].detail}</span>
              </div>
            </div>
          ))}
        </div>
      </div>

      {/* ---------------------------------------------------------------
          THE EXACT MANIFESTS, from the server's dry run.

          This is invariant (a) made visible before the fact. The same
          render function produces these and the files the executor will
          write to tenants/<project>/rendered/, so what a PI reads here is
          byte-for-byte what gets created — not an illustration of it.
          Collapsed by default: most people do not want it, and the ones
          who do really want it.
          --------------------------------------------------------------- */}
      <div className="card">
        <button
          type="button"
          className="disclose"
          onClick={() => setOpen((o) => !o)}
          aria-expanded={open}
        >
          <span className="disclose__caret">{open ? "▾" : "▸"}</span>
          Exact resources to be created
          <span className="small muted">{files ? `${files.length} files` : ""}</span>
        </button>

        {open && (
          <>
            {filesLoading && <p className="small muted">Rendering the manifests…</p>}
            {filesError && (
              <div className="state state--error">
                <div className="state__title">Could not render a preview</div>
                <p className="small">{filesError.message}</p>
                <button type="button" className="btn btn--ghost btn--sm" onClick={onReloadFiles}>
                  Retry
                </button>
              </div>
            )}
            {files && files.map((f) => <YamlFile key={f.name} file={f} />)}
            <p className="small muted">
              Read-only. These are written into <code>tenants/{values.projectName}/rendered/</code>{" "}
              and applied from there, so your project is as reviewable as a hand-written one.
            </p>
          </>
        )}
      </div>
    </>
  );
}

// ---------------------------------------------------------------------------
// THE WIZARD
// ---------------------------------------------------------------------------

export default function Wizard() {
  const { id } = useParams();
  const navigate = useNavigate();
  const templateState = useFetch(`/api/templates/${id}`, "Loading the questions…");
  const modelsState = useFetch("/api/models", "Loading models…");
  const mode = useExecutorMode();

  const [step, setStep] = useState(0);
  // Only what the PI has actually changed. The template's defaults are folded in
  // below rather than copied into state, so there is no moment where the form
  // exists but has not been seeded — see `values`.
  const [edits, setEdits] = useState(null);
  const [touched, setTouched] = useState({});
  const [serverErrors, setServerErrors] = useState({});
  const [submitting, setSubmitting] = useState(false);
  const [submitError, setSubmitError] = useState(null);

  const [files, setFiles] = useState(null);
  const [filesError, setFilesError] = useState(null);
  const [filesLoading, setFilesLoading] = useState(false);

  // What the server found inside the uploaded dataset: its format, its columns,
  // a row estimate and the first few rows. Null until something is uploaded.
  const [uploadPreview, setUploadPreview] = useState(null);

  const template = templateState.data;
  const params = useMemo(() => template?.spec?.parameters || [], [template]);
  const models = modelsState.data || [];

  // THE TEMPLATE'S OWN DEFAULTS, derived rather than copied into state.
  //
  // The old version hard-coded {availability, tier, durationDays}, so a template
  // with different defaults silently inherited the first template's. Seeding
  // state from the template in an effect fixes that but introduces a frame where
  // the template has loaded and the form has not been filled in — a blank flash
  // on every visit. Deriving it means the defaults are simply always there.
  const defaults = useMemo(() => {
    const seeded = {};
    for (const p of params) {
      if (p.default != null) seeded[p.name] = p.default;
      // An upload answers with an id and starts with none. Explicitly "" rather
      // than absent, so the review screen can tell "no documents" apart from
      // "this template does not ask for documents".
      else if (p.type === "upload") seeded[p.name] = "";
    }
    return seeded;
  }, [params]);

  const values = useMemo(() => ({ ...defaults, ...(edits || {}) }), [defaults, edits]);

  const set = useCallback((key, value) => {
    setEdits((v) => ({ ...(v || {}), [key]: value }));
    // A field the PI is actively fixing must lose its server error immediately;
    // leaving it would keep showing a complaint about a value that is gone.
    setServerErrors((e) => (e[key] ? { ...e, [key]: undefined } : e));
  }, []);

  const touch = useCallback((key) => setTouched((t) => ({ ...t, [key]: true })), []);

  // -------------------------------------------------------------------------
  // WHAT IS INSIDE THE DATASET
  // -------------------------------------------------------------------------
  // Some questions are about the PI's own file rather than about the platform:
  // batch-inference asks which columns the model reads, and the answer only
  // exists once there is a file to read them from. The template therefore
  // declares that parameter with NO `values` — there is nothing the catalogue
  // could have listed — and the options are supplied here.
  //
  // FETCHED FROM THE SERVER'S MANIFEST, not lifted out of the upload widget,
  // for the same reason CorpusSummary does it: the server has already parsed
  // the file that will actually be copied into the project, and a list the
  // browser assembled from the local File object would be a different file's
  // columns the moment sanitisation renamed something.

  /** The upload parameter this template asks for, if it asks for one at all. */
  const uploadParam = useMemo(
    () => params.find((p) => p.type === "upload") || null,
    [params],
  );
  const uploadId = uploadParam ? String(values[uploadParam.name] || "").trim() : "";

  useEffect(() => {
    if (!uploadId) {
      setUploadPreview(null);
      return;
    }
    let cancelled = false;
    get(`/api/uploads/${uploadId}`)
      .then((body) => !cancelled && setUploadPreview(body.preview || null))
      // A preview that cannot be fetched is not an error worth a red panel: the
      // upload itself already succeeded, and the field below simply falls back
      // to its empty state, which explains what to do.
      .catch(() => !cancelled && setUploadPreview(null));
    return () => {
      cancelled = true;
    };
  }, [uploadId]);

  // THE SAMPLE'S SHAPE, WHEN NOTHING WAS UPLOADED.
  //
  // A template whose upload is optional promises "leave it empty and we use a
  // small sample". That promise was impossible to take: the column question is
  // REQUIRED, and with no file there were no columns to pick, so the wizard
  // could not be finished at all without uploading something.
  //
  // The server ships each sample's preview with the template, keyed by model
  // interface — the same key its executor uses to choose the file — so the
  // columns offered here are the columns of the file the run will really read.
  const chosenModel = useMemo(
    () => models.find((m) => m.id === (values.model || values.baseModel)) || null,
    [models, values.model, values.baseModel],
  );
  const sampleEntry = useMemo(() => {
    if (!template?.sampleDatasets || !chosenModel) return null;
    return template.sampleDatasets[chosenModel.interface] || null;
  }, [template, chosenModel]);
  const samplePreview = sampleEntry?.preview || null;
  const sampleFile = sampleEntry?.file || null;

  /** The dataset in play: the PI's upload, or the sample that stands in for it. */
  const dataset = uploadPreview || samplePreview;

  // THE FORMAT IS DETECTED, NOT ASKED. The template's help text already says so
  // ("The upload preview detects this for you"), and a PI who uploads a .jsonl
  // and leaves the format on its `csv` default gets a run where every row fails
  // to parse. Only ever moved to match the file in hand, and still editable —
  // it is a pre-filled answer, not a locked one.
  useEffect(() => {
    if (!uploadPreview?.format) return;
    setEdits((v) =>
      (v || {}).inputFormat === uploadPreview.format
        ? v
        : { ...(v || {}), inputFormat: uploadPreview.format },
    );
  }, [uploadPreview]);

  /**
   * The options for one parameter, or undefined to use the template's own.
   *
   * A multiselect the CATALOGUE CANNOT POPULATE — no inline `values`, no
   * `source` to resolve from — is one whose choices come from the PI's own
   * file. That is the rule rather than a check for the name `inputFields`,
   * because it is the reason the parameter has no values: the catalogue cannot
   * know what is inside a file that did not exist when it was written.
   */
  const optionsFor = useCallback(
    (param) => {
      if (param.type !== "multiselect") return undefined;
      if ((param.values || []).length || param.source) return undefined;
      return dataset?.columns || [];
    },
    [dataset],
  );

  const clientErrors = useMemo(() => {
    if (!params.length) return {};
    if (step === 0) return checkGroup(params, values, "basics");
    if (step === 1) return checkGroup(params, values, "resources");
    return {};
  }, [step, values, params]);

  // Errors are shown once a field has been left (blur) or once the PI has tried
  // to move on — never while they are still typing the first character.
  const shown = {};
  for (const key of Object.keys({ ...clientErrors, ...serverErrors })) {
    if (serverErrors[key]) shown[key] = serverErrors[key];
    else if (touched[key]) shown[key] = clientErrors[key];
  }

  const stepValid = Object.keys(clientErrors).length === 0;

  const loadPreview = useCallback(async () => {
    setFilesLoading(true);
    setFilesError(null);
    try {
      const body = await post(`/api/deployments?dry_run=1`, { templateId: id, params: values });
      setFiles(body.files);
    } catch (err) {
      setFilesError(err);
    } finally {
      setFilesLoading(false);
    }
  }, [id, values]);

  function goNext() {
    if (!stepValid) {
      // Reveal every problem on this screen at once rather than one per attempt.
      setTouched((t) => ({
        ...t,
        ...Object.fromEntries(Object.keys(clientErrors).map((k) => [k, true])),
      }));
      return;
    }
    const next = step + 1;
    setStep(next);
    if (next === 2) loadPreview();
  }

  async function deploy() {
    setSubmitting(true);
    setSubmitError(null);
    setServerErrors({});
    try {
      const body = await post("/api/deployments", { templateId: id, params: values });
      navigate(`/deployments/${body.id}`);
    } catch (err) {
      if (err instanceof ApiError && err.status === 400 && err.fieldErrors.length) {
        // Map the server's rejections onto the exact inputs they belong to, and
        // jump back to the screen holding the first one. A field error shown on
        // a page the PI cannot see is the same as no error at all.
        //
        // WHICH screen that is comes from the parameter's own `group`, so this
        // works for a template whose questions this file has never seen.
        const mapped = {};
        for (const e of err.fieldErrors) mapped[e.field] = e.message;
        setServerErrors(mapped);
        setTouched((t) => ({
          ...t,
          ...Object.fromEntries(Object.keys(mapped).map((k) => [k, true])),
        }));
        const groupOf = (name) =>
          params.find((p) => p.name === name)?.group || "resources";
        const anyBasics = Object.keys(mapped).some((f) => groupOf(f) === "basics");
        setStep(anyBasics ? 0 : 1);
      } else {
        setSubmitError(err);
      }
    } finally {
      setSubmitting(false);
    }
  }

  /**
   * The read-only consequences shown under specific answers.
   *
   * Presentation only — every one of these follows a generic input rather than
   * replacing it, and a template without the parameter simply gets nothing.
   */
  // `mode` is a dependency: the derived hostname preview differs between modes,
  // and a callback memoised without it would keep rendering the mock host after
  // the health check resolved.
  const after = useCallback((param, current) => {
    if (param.name === "projectName") {
      const name = (current.projectName || "").trim();
      return name && !checkParam(param, name) ? (
        <DerivedNames name={name} mode={mode} />
      ) : null;
    }
    if (param.name === "tier") return <TierCeiling tier={current.tier} />;
    // Keyed by TYPE, not by name: what follows an upload field is a description
    // of the file it produced, and that is true of any upload a template asks
    // for. `sampleName` is what makes the fallback visible rather than implied.
    if (param.type === "upload" && dataset) {
      return (
        <DatasetShape
          preview={dataset}
          sample={uploadPreview ? null : sampleFile}
        />
      );
    }
    return null;
  }, [mode, dataset, uploadPreview, sampleFile]);

  return (
    <div className="page">
      <Fetched state={templateState}>
        {(tpl) => (
          <Fetched state={modelsState}>
            {() => (
              <>
                  <div className="page__head">
                    <h1>{tpl.metadata.displayName}</h1>
                    <p>Answer three short screens. Nothing is created until you press Deploy.</p>
                  </div>

                  <StepRail current={step} />

                  {step === 0 && (
                    <QuestionScreen
                      params={params}
                      group="basics"
                      values={values}
                      set={set}
                      errors={shown}
                      touch={touch}
                      after={after}
                      optionsFor={optionsFor}
                    />
                  )}
                  {step === 1 && (
                    <QuestionScreen
                      params={params}
                      group="resources"
                      values={values}
                      set={set}
                      errors={shown}
                      touch={touch}
                      after={after}
                      optionsFor={optionsFor}
                    />
                  )}
                  {step === 2 && (
                    <Review
                      template={tpl}
                      values={values}
                      models={models}
                      files={files}
                      filesError={filesError}
                      filesLoading={filesLoading}
                      onReloadFiles={loadPreview}
                    />
                  )}

                  {submitError && (
                    <div className="state state--error" role="alert">
                      <div className="state__title">Could not start your project</div>
                      <p className="small">{submitError.message}</p>
                    </div>
                  )}

                  <div className="wizard__foot">
                    {step > 0 ? (
                      <button
                        type="button"
                        className="btn btn--ghost"
                        onClick={() => setStep(step - 1)}
                      >
                        Back
                      </button>
                    ) : (
                      <Link className="btn btn--ghost" to={`/templates/${id}`}>
                        Cancel
                      </Link>
                    )}

                    {step < 2 ? (
                      <button type="button" className="btn btn--primary" onClick={goNext}>
                        Continue
                      </button>
                    ) : (
                      <button
                        type="button"
                        className="btn btn--primary"
                        onClick={deploy}
                        disabled={submitting}
                      >
                        {submitting ? "Starting…" : "Deploy"}
                      </button>
                    )}
                </div>
              </>
            )}
          </Fetched>
        )}
      </Fetched>
    </div>
  );
}
